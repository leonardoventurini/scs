"""Read-only repository path validation for MCP tool inputs."""

from __future__ import annotations

from pathlib import Path


def canonical_repo_path(repo_path: str | None) -> str | None:
    """Resolve a repository scope without creating or changing it."""

    if repo_path == "":
        raise ValueError("repo_path must be a non-empty string")
    candidate = Path.cwd() if repo_path is None else Path(repo_path)
    try:
        resolved = candidate.expanduser().resolve(strict=True)
    except OSError as error:
        raise ValueError(f"repository path is unavailable: {candidate}") from error
    if not resolved.is_dir():
        raise ValueError(f"repository path is not a directory: {resolved}")
    return str(resolved)


def contained_file_path(file_path: str, repo_path: str | None = None) -> str:
    """Resolve a source path and reject escapes from an explicit repository."""

    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute() and repo_path is not None:
        candidate = Path(repo_path) / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"source path does not exist: {candidate}") from error
    if not resolved.is_file():
        raise ValueError(f"source path is not a file: {resolved}")
    if repo_path is not None and not resolved.is_relative_to(Path(repo_path)):
        raise ValueError(f"source path escapes repository: {resolved}")
    return str(resolved)


def contained_deleted_path(file_path: str) -> str:
    """Validate a repository-relative deletion path without touching disk."""

    path = Path(file_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"deleted path must remain repository-relative: {file_path}")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ValueError("deleted path must identify a file")
    return normalized
