"""Read-only repository path validation for MCP tool inputs."""

from __future__ import annotations

from pathlib import Path

from scs.source_paths import validated_source_path


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
    """Validate a source target while preserving its indexed alias identity."""

    return validated_source_path(file_path, repo_path)


def contained_deleted_path(file_path: str) -> str:
    """Validate a repository-relative deletion path without touching disk."""

    path = Path(file_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"deleted path must remain repository-relative: {file_path}")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ValueError("deleted path must identify a file")
    return normalized
