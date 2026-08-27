"""Read-only repository watcher that queues changes only for indexed roots."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from watchfiles import Change

from scs.indexing.jobs import IngestionJobStore
from scs.indexing.repository_paths import canonicalize_repo_path

IGNORED_DIRECTORIES = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", "build", "dist", "target"}
)
MASS_CHANGE_THRESHOLD = 32


class IndexedRepoResolver(Protocol):
    """Read-only repository lookup required by watcher routing."""

    def resolve_repo_id_sync(self, path: str) -> int | None: ...


class _WatchfilesModule(Protocol):
    """Typed surface used from the lazily loaded watcher dependency."""

    awatch: Callable[..., AsyncIterator[set[tuple[Change, str]]]]


@dataclass(slots=True)
class PendingChanges:
    """Debounced paths for one already-indexed repository."""

    changed: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    requires_full: bool = False


class RepositoryWatcher:
    """Translate filesystem events into durable explicit-repository jobs."""

    def __init__(
        self,
        *,
        graph: IndexedRepoResolver,
        jobs: IngestionJobStore,
        base_dir: Path,
        supported_extensions: frozenset[str],
        store_id: str | None = None,
        store_generation: str | None = None,
        debounce_seconds: float = 0.75,
        mass_change_threshold: int = MASS_CHANGE_THRESHOLD,
    ) -> None:
        self._graph: IndexedRepoResolver = graph
        self._jobs: IngestionJobStore = jobs
        self._base_dir: Path = base_dir.expanduser().resolve()
        self._extensions: frozenset[str] = supported_extensions
        self._store_id: str | None = store_id
        self._store_generation: str | None = store_generation
        self._debounce: float = debounce_seconds
        self._mass_threshold: int = mass_change_threshold
        self._pending: dict[str, PendingChanges] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._stop: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._lock: asyncio.Lock = asyncio.Lock()

    async def start(self) -> None:
        """Start observation without enqueuing startup or discovery work."""

        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._watch(), name="scs-repository-watcher")

    async def stop(self) -> None:
        """Cancel observation and pending debounce tasks."""

        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        timers, self._timers = list(self._timers.values()), {}
        for timer in timers:
            timer.cancel()
        await asyncio.gather(*timers, return_exceptions=True)

    async def _watch(self) -> None:
        watchfiles = cast(_WatchfilesModule, importlib.import_module("watchfiles"))

        async for changes in watchfiles.awatch(
            self._base_dir, stop_event=self._stop, recursive=True
        ):
            await self.record(changes)

    async def record(self, changes: Iterable[tuple[Change | int, str]]) -> None:
        """Route supported source changes to the nearest indexed Git root."""

        async with self._lock:
            for change, raw_path in changes:
                path = Path(raw_path).resolve(strict=False)
                repo = self._repository_for(path)
                if repo is None:
                    continue
                repo_str = canonicalize_repo_path(repo)
                if self._graph.resolve_repo_id_sync(repo_str) is None:
                    continue
                relative = path.relative_to(repo).as_posix()
                pending = self._pending.setdefault(repo_str, PendingChanges())
                if path.name == ".gitignore":
                    pending.requires_full = True
                elif path.suffix not in self._extensions or any(
                    part in IGNORED_DIRECTORIES for part in path.parts
                ):
                    continue
                elif int(change) == 3:
                    pending.changed.discard(str(path))
                    pending.deleted.add(relative)
                else:
                    pending.deleted.discard(relative)
                    pending.changed.add(str(path))
                previous = self._timers.get(repo_str)
                if previous is not None:
                    previous.cancel()
                self._timers[repo_str] = asyncio.create_task(
                    self._flush_later(repo_str)
                )

    def _repository_for(self, path: Path) -> Path | None:
        current = path.parent
        while current != self._base_dir and current != current.parent:
            try:
                current.relative_to(self._base_dir)
            except ValueError:
                return None
            if (current / ".git").exists():
                return current
            current = current.parent
        return self._base_dir if (self._base_dir / ".git").exists() else None

    async def _flush_later(self, repo_path: str) -> None:
        try:
            await asyncio.sleep(self._debounce)
            async with self._lock:
                pending = self._pending.pop(repo_path, PendingChanges())
                self._timers.pop(repo_path, None)
            if (
                pending.requires_full
                or len(pending.changed) + len(pending.deleted) >= self._mass_threshold
            ):
                await asyncio.to_thread(
                    self._jobs.enqueue,
                    repo_path=repo_path,
                    store_id=self._store_id,
                    store_generation=self._store_generation,
                    mode="full",
                    reason="watcher-full",
                )
            elif pending.changed or pending.deleted:
                await asyncio.to_thread(
                    self._jobs.enqueue,
                    repo_path=repo_path,
                    store_id=self._store_id,
                    store_generation=self._store_generation,
                    mode="files",
                    reason="watcher-files",
                    payload={
                        "file_paths": sorted(pending.changed),
                        "deleted_paths": sorted(pending.deleted),
                    },
                )
        except asyncio.CancelledError:
            raise
