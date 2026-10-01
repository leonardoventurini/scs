"""Remove the executing uv tool after acquiring exclusive lifecycle ownership."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from scs.config import SCSSettings
from scs.daemon import DaemonController
from scs.paths import default_log_directory, default_runtime_directory
from scs.service import ProcessLock
from scs.storage.catalog import ProjectStoreCatalog

TOOL_NAME = "scs"
UV_RECEIPT = "uv-receipt.toml"
REPOSITORY_MARKERS = frozenset({".git", ".hg", ".svn"})
SYSTEM_CONTAINERS = (
    "/",
    "/Applications",
    "/Library",
    "/System",
    "/Users",
    "/Volumes",
    "/bin",
    "/etc",
    "/home",
    "/opt",
    "/opt/homebrew",
    "/private",
    "/sbin",
    "/tmp",
    "/usr",
    "/usr/local",
    "/var",
)
SHARED_HOME_DIRECTORIES = (
    ".cache",
    ".config",
    ".local",
    "Desktop",
    "Documents",
    "Downloads",
    "Library",
    "Music",
    "Pictures",
    "Public",
    "Repositories",
    "Videos",
)


class UninstallError(RuntimeError):
    """Uninstallation could not complete; the message identifies its stage."""


@dataclass(frozen=True, slots=True)
class _Installation:
    uv: str
    root: Path


@dataclass(frozen=True, slots=True)
class _PurgePlan:
    directories: tuple[Path, ...]
    config: Path


def _installation() -> _Installation:
    uv = shutil.which("uv")
    if uv is None:
        raise UninstallError(
            "uv is required on PATH; install uv before uninstalling SCS"
        )

    root = Path(sys.prefix).resolve()
    if (
        root.name != TOOL_NAME
        or not (root / UV_RECEIPT).is_file()
        or not Path(__file__).resolve().is_relative_to(root)
    ):
        raise UninstallError(
            "run uninstall with the uv-installed SCS executable; "
            "source-tree, pip, and pipx environments are not supported"
        )
    return _Installation(uv=uv, root=root)


def _raise_walk_error(error: OSError) -> None:
    raise error


def _purge_plan(settings: SCSSettings, installation: _Installation) -> _PurgePlan:
    user_home = Path.home().resolve()
    config_directory = Path.home() / ".scs"
    config = config_directory / "config.toml"
    if config_directory.is_symlink() or config.is_symlink():
        raise UninstallError(f"unsafe purge configuration symlink: {config}")

    protected = {
        user_home,
        Path.cwd().resolve(),
        installation.root.parent,
        Path(installation.uv).resolve(),
        Path(sys.base_prefix).resolve(),
        *((user_home / name).resolve() for name in SHARED_HOME_DIRECTORIES),
        default_log_directory().resolve().parent,
        default_runtime_directory().resolve().parent,
        *(Path(path).resolve() for path in SYSTEM_CONTAINERS),
    }
    source_roots = tuple(
        Path(record.canonical_root).resolve()
        for record in ProjectStoreCatalog(settings.home, migrate=False).list_records()
    )
    directories: set[Path] = set()
    for configured in (
        settings.home,
        settings.model_cache,
        settings.log_dir,
        settings.runtime_dir,
    ):
        raw = configured.expanduser()
        directory = raw.resolve()
        if raw.is_symlink():
            raise UninstallError(f"unsafe purge directory symlink: {raw}")
        if (
            any(path.is_relative_to(directory) for path in protected)
            or directory.is_relative_to(installation.root.parent)
            or directory.is_relative_to(Path(sys.base_prefix).resolve())
            or any(
                root.is_relative_to(directory) or directory.is_relative_to(root)
                for root in source_roots
            )
        ):
            raise UninstallError(
                f"unsafe purge directory overlaps protected files: {raw}"
            )
        if directory.exists() and not directory.is_dir():
            raise UninstallError(f"purge path is not a directory: {raw}")

        # A configured state directory must not also contain repository source.
        # Do not follow internal symlinks: cleanup unlinks them, not their targets.
        ancestors = (directory, *directory.parents)
        if any(
            (parent / marker).exists()
            for parent in ancestors
            for marker in REPOSITORY_MARKERS
        ):
            raise UninstallError(
                f"unsafe purge directory is inside a repository: {raw}"
            )
        if directory.exists():
            for root, children, files in directory.walk(on_error=_raise_walk_error):
                if REPOSITORY_MARKERS.intersection((*children, *files)):
                    raise UninstallError(
                        f"unsafe purge directory contains a repository: {root}"
                    )
        directories.add(directory)

    # If runtime/cache/logs live inside the data root, clean their common root once.
    roots = tuple(
        directory
        for directory in sorted(directories, key=str)
        if not any(parent in directories for parent in directory.parents)
    )
    return _PurgePlan(directories=roots, config=config)


def _purge_directory(directory: Path, retained_locks: tuple[Path, ...]) -> None:
    if not directory.exists():
        return
    if directory.is_symlink():
        raise UninstallError(f"purge directory became a symlink: {directory}")

    for entry in directory.iterdir():
        if entry in retained_locks:
            continue
        if any(lock.is_relative_to(entry) for lock in retained_locks):
            _purge_directory(entry, retained_locks)
        elif entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)

    # Never replace a lock inode: another process may already be waiting on it.
    if not any(lock.is_relative_to(directory) for lock in retained_locks):
        directory.rmdir()


def uninstall(settings: SCSSettings, *, purge: bool = False) -> None:
    """Stop SCS and remove its executing uv installation, optionally purging state.

    Validation runs before shutdown. Package-removal failure preserves data;
    cleanup failure after removal reports paths for manual recovery. Bootstrap
    and writer locks remain on disk so concurrent contenders share their inodes.
    MCP registrations and repository source are never modified.
    """

    installation = _installation()
    try:
        plan = _purge_plan(settings, installation) if purge else None
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        raise UninstallError(f"purge validation failed: {error}") from error

    paths = settings.paths
    bootstrap_path = paths.runtime / ".bootstrap.lock"
    writer_path = paths.home / ".daemon.lock"
    for lock_path in (bootstrap_path, writer_path):
        if lock_path.is_symlink():
            raise UninstallError(f"unsafe lifecycle lock symlink: {lock_path}")

    stage = "lifecycle shutdown"
    try:
        with ProcessLock(bootstrap_path):
            asyncio.run(DaemonController(settings).stop(cancel_active=True))
            with ProcessLock(writer_path):
                if plan is not None and _purge_plan(settings, installation) != plan:
                    raise UninstallError("purge paths changed during shutdown")

                stage = "package removal"
                # Select the executing tool, even when the caller has changed
                # UV_TOOL_DIR since installation. uv's receipt owns its launchers.
                environment = os.environ | {
                    "UV_TOOL_DIR": str(installation.root.parent)
                }
                _ = subprocess.run(
                    [installation.uv, "tool", "uninstall", TOOL_NAME],
                    env=environment,
                    check=True,
                )

                if plan is not None:
                    stage = "package removed; cleanup"
                    for directory in plan.directories:
                        _purge_directory(directory, (bootstrap_path, writer_path))
                    plan.config.unlink(missing_ok=True)
    except (
        OSError,
        RuntimeError,
        ValueError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        recovery = ""
        if stage == "package removed; cleanup" and plan is not None:
            remaining = [
                str(path) for path in (*plan.directories, plan.config) if path.exists()
            ]
            recovery = (
                f"; inspect remaining paths for manual cleanup: {', '.join(remaining)}"
            )
        raise UninstallError(f"{stage} failed: {error}{recovery}") from error
