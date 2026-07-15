"""File discovery for code ingestion, respecting Git ignore rules.

Walks the directory tree, filters candidates through Git's ignore
engine when available, and computes content hashes for incremental
change detection.
"""

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pathspec

from scs.indexing.parser.native import NativeParser

logger = logging.getLogger(__name__)


def supported_extensions() -> frozenset[str]:
    """Return extensions from SCS's native parser registry."""

    return NativeParser().supported_extensions()

HASH_CHUNK_SIZE_BYTES = 65536
GIT_CHECK_IGNORE_TIMEOUT_SECONDS = 5.0
GIT_CHECK_IGNORE_COMMAND: tuple[str, ...] = ("git", "check-ignore", "--stdin", "-z")
GIT_LS_FILES_TIMEOUT_SECONDS = 30.0
GIT_LS_FILES_COMMAND: tuple[str, ...] = ("git", "ls-files", "-co", "--exclude-standard", "-z")

# Directories always skipped regardless of .gitignore.
# These are either VCS internals, build artifacts, or virtual environments
# that should never be indexed.
ALWAYS_SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    ".ci-cargo",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "DerivedData",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "eggs",
    ".eggs",
    "_build",  # Elixir/Mix build directory.
    "deps",    # Elixir/Mix dependency directory.
})


@dataclass
class FileEntry:
    """A discovered source file ready for parsing.

    Attributes:
        rel_path: Path relative to the repository root.
        abs_path: Absolute filesystem path.
        language: Programming language derived from extension.
        byte_size: File size in bytes.
        content_hash: SHA-256 hash of file contents for change detection.
    """

    rel_path: str
    abs_path: Path
    language: str
    byte_size: int
    content_hash: str


# Extension to language name mapping for FileEntry.language.
_EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".rs": "rust",
    ".ex": "elixir",
    ".exs": "elixir",
    ".sh": "bash",
    ".bash": "bash",
    ".css": "css",
}


def _load_gitignore_spec(repo_path: Path) -> pathspec.PathSpec | None:
    """Load root ``.gitignore`` patterns as a fallback matcher.

    Git is the source of truth when available because it understands
    nested ``.gitignore`` files, ``.git/info/exclude``, and global
    excludes. This pathspec-based fallback preserves the older behavior
    when discovery runs outside a Git worktree or Git is unavailable.
    """
    gitignore = repo_path / ".gitignore"
    if not gitignore.exists():
        return None

    try:
        text = gitignore.read_text()
        return pathspec.PathSpec.from_lines("gitignore", text.splitlines())
    except OSError:
        logger.warning("Failed to read .gitignore at %s", gitignore)
        return None


def _compute_hash(file_path: Path) -> str:
    """Compute SHA-256 hash of a file's contents for change detection.

    Reads the file in 64KB chunks to handle large files without
    excessive memory usage.
    """
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(HASH_CHUNK_SIZE_BYTES):
            hasher.update(chunk)
    return hasher.hexdigest()


def _git_check_ignored_paths(repo_path: Path, rel_paths: list[str]) -> set[str] | None:
    """Return ignored repo-relative paths according to Git.

    Returns ``None`` when Git cannot answer the query so callers can
    fall back to the root ``.gitignore`` matcher.
    """
    if not rel_paths:
        return set()

    encoded_input = b"\0".join(path.encode("utf-8", errors="surrogateescape") for path in rel_paths)
    encoded_input += b"\0"

    try:
        result = subprocess.run(
            GIT_CHECK_IGNORE_COMMAND,
            cwd=repo_path,
            input=encoded_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=GIT_CHECK_IGNORE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Git ignore lookup unavailable for %s: %s", repo_path, exc)
        return None

    if result.returncode not in (0, 1):
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        logger.debug(
            "Git ignore lookup failed for %s with exit code %s: %s",
            repo_path,
            result.returncode,
            stderr or "<no stderr>",
        )
        return None

    if not result.stdout:
        return set()

    return {
        entry.decode("utf-8", errors="surrogateescape")
        for entry in result.stdout.split(b"\0")
        if entry
    }


def _resolve_ignored_paths(
    repo_path: Path,
    rel_paths: list[str],
    fallback_spec: pathspec.PathSpec | None,
) -> set[str]:
    """Resolve ignored paths using Git first, then the legacy fallback."""
    ignored_paths = _git_check_ignored_paths(repo_path, rel_paths)
    if ignored_paths is not None:
        return ignored_paths

    if fallback_spec is None:
        return set()

    return {rel_path for rel_path in rel_paths if fallback_spec.match_file(rel_path)}


def _list_git_non_ignored_paths(repo_path: Path) -> list[str] | None:
    """Return repo-relative paths that Git considers visible.

    Git already knows about nested ``.gitignore`` files, ``.git/info/exclude``,
    and global ignore configuration. Using ``git ls-files`` keeps repo-wide
    discovery deterministic and avoids the timeout-sensitive candidate scan
    that ``git check-ignore`` required.
    """
    try:
        result = subprocess.run(
            GIT_LS_FILES_COMMAND,
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=GIT_LS_FILES_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Git file listing unavailable for %s: %s", repo_path, exc)
        return None

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        logger.debug(
            "Git file listing failed for %s with exit code %s: %s",
            repo_path,
            result.returncode,
            stderr or "<no stderr>",
        )
        return None

    if not result.stdout:
        return []

    return [
        entry.decode("utf-8", errors="surrogateescape")
        for entry in result.stdout.split(b"\0")
        if entry
    ]


def _is_in_always_skipped_directory(rel_path: Path) -> bool:
    """Return whether a repo-relative path lives in an always-skipped directory."""
    return any(part in ALWAYS_SKIP_DIRS for part in rel_path.parts[:-1])


def build_file_entry(
    file_path: Path,
    repo_path: Path,
    extensions: frozenset[str] | None = None,
) -> FileEntry | None:
    """Build a FileEntry for a single file without walking the directory tree.

    Reuses the same extension-to-language mapping, hash computation, and
    skip-directory checks as ``discover()``, but operates on a single
    absolute path. Designed for incremental ingestion after Claude Code
    edits a file — avoids the full ``rglob`` walk.

    Returns None if the file doesn't exist, has an unsupported extension,
    or is in an always-skipped directory.
    """
    file_path = file_path.resolve()
    repo_path = repo_path.resolve()

    # The file must exist and be a regular file (not a directory or symlink to one).
    if not file_path.is_file():
        return None

    # Compute the relative path from the repo root.
    try:
        rel_file_path = file_path.relative_to(repo_path)
    except ValueError:
        # File is not under repo_path — cannot compute a relative path.
        logger.warning("File %s is not under repo %s — skipping", file_path, repo_path)
        return None

    # Skip files in always-excluded directories (e.g., .venv, node_modules).
    if _is_in_always_skipped_directory(rel_file_path):
        return None

    # Skip unsupported extensions (must match the parser registry).
    extensions = extensions or supported_extensions()
    if file_path.suffix not in extensions:
        return None

    rel_path = rel_file_path.as_posix()

    # Respect Git ignore rules, with a root .gitignore fallback outside Git repos.
    fallback_spec = _load_gitignore_spec(repo_path)
    ignored_paths = _resolve_ignored_paths(repo_path, [rel_path], fallback_spec)
    if rel_path in ignored_paths:
        return None

    # Compute content hash and file size.
    try:
        byte_size = file_path.stat().st_size
        content_hash = _compute_hash(file_path)
    except OSError:
        logger.warning("Failed to read %s — skipping", file_path)
        return None

    language = _EXTENSION_TO_LANGUAGE.get(file_path.suffix, "unknown")

    return FileEntry(
        rel_path=rel_path,
        abs_path=file_path,
        language=language,
        byte_size=byte_size,
        content_hash=content_hash,
    )


def discover(
    repo_path: Path,
    extensions: frozenset[str] | None = None,
    skip_always_dirs: bool = True,
) -> list[FileEntry]:
    """Walk the repository and discover all parseable source files.

    Uses Git's own file listing when available so nested ``.gitignore``
    files, ``.git/info/exclude``, and global ignores are handled by the
    same engine that owns the repository. The Git fast path is only used
    when ``skip_always_dirs`` is enabled; library-ingestion callers that
    intentionally include ``node_modules``/``.venv`` still use the
    filesystem walk. Falls back to a filesystem walk with a root
    ``.gitignore`` matcher when Git is unavailable.

    Args:
        repo_path: Root directory to scan.
        skip_always_dirs: If True (default), skip directories in
            ALWAYS_SKIP_DIRS (.venv, node_modules, etc.). Set to False
            for library ingestion where source files live inside these
            directories.
    """
    repo_path = repo_path.resolve()
    fallback_spec = _load_gitignore_spec(repo_path)
    extensions = extensions or supported_extensions()
    entries: list[FileEntry] = []
    candidate_rel_paths = (
        _list_git_non_ignored_paths(repo_path) if skip_always_dirs else None
    )

    if candidate_rel_paths is None:
        candidates: list[tuple[Path, str]] = []
        for file_path in repo_path.rglob("*"):
            # Skip directories.
            if file_path.is_dir():
                continue

            # Skip files in always-excluded directories (unless disabled
            # for library ingestion where source IS inside venv/node_modules).
            rel_file_path = file_path.relative_to(repo_path)

            if skip_always_dirs and _is_in_always_skipped_directory(rel_file_path):
                continue

            # Skip unsupported extensions.
            if file_path.suffix not in extensions:
                continue

            candidates.append((file_path, rel_file_path.as_posix()))

        ignored_paths = _resolve_ignored_paths(
            repo_path,
            [rel_path for _, rel_path in candidates],
            fallback_spec,
        )
        iterable = (
            (file_path, rel_path)
            for file_path, rel_path in candidates
            if rel_path not in ignored_paths
        )
    else:
        iterable = (
            (repo_path / rel_path, rel_path)
            for rel_path in candidate_rel_paths
        )

    for file_path, rel_path in iterable:
        # Skip directories, broken symlinks, and files in always-excluded
        # directories (unless disabled for library ingestion where source
        # IS inside venv/node_modules).
        if not file_path.is_file():
            continue

        rel_file_path = file_path.relative_to(repo_path)
        if skip_always_dirs and _is_in_always_skipped_directory(rel_file_path):
            continue

        # Skip unsupported extensions.
        if file_path.suffix not in extensions:
            continue

        # Compute content hash and file size.
        try:
            byte_size = file_path.stat().st_size
            content_hash = _compute_hash(file_path)
        except OSError:
            logger.warning("Failed to read %s — skipping", file_path)
            continue

        language = _EXTENSION_TO_LANGUAGE.get(file_path.suffix, "unknown")

        entries.append(
            FileEntry(
                rel_path=rel_path,
                abs_path=file_path,
                language=language,
                byte_size=byte_size,
                content_hash=content_hash,
            )
        )

    logger.info("Discovered %d source files in %s", len(entries), repo_path)
    return entries
