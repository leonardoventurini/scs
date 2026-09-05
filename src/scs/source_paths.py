"""Shared source identity and target-containment validation."""

from __future__ import annotations

import os
from pathlib import Path


def lexical_file_in_repo(file_path: Path, repo_path: Path) -> Path | None:
    """Normalize root aliases while retaining aliases beneath the repository."""

    absolute = Path(os.path.abspath(file_path))
    try:
        return repo_path / absolute.relative_to(repo_path)
    except ValueError:
        # A caller may use a symlinked checkout root or macOS /var spelling.
        # Resolve ancestors only until the repository boundary is identified.
        for parent in absolute.parents:
            try:
                if parent.resolve() == repo_path:
                    return repo_path / absolute.relative_to(parent)
            except OSError, RuntimeError:
                continue
        return None


def validated_source_path(
    file_path: str,
    repo_path: str | None = None,
    *,
    require_file: bool = True,
) -> str:
    """Retain a lexical source identity while checking its resolved target."""

    candidate = Path(file_path).expanduser()
    root = Path(repo_path).resolve() if repo_path is not None else None
    if not candidate.is_absolute() and root is not None:
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=require_file)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"source path does not exist: {candidate}") from error
    if require_file and not resolved.is_file():
        raise ValueError(f"source path is not a file: {resolved}")
    if root is None:
        return str(Path(os.path.abspath(candidate)))
    lexical = lexical_file_in_repo(candidate, root)
    if lexical is None or not resolved.is_relative_to(root):
        raise ValueError(f"source path escapes repository: {resolved}")
    return str(lexical)
