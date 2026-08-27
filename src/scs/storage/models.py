"""Typed identities and lifecycle state for one project store."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from pathlib import Path
from typing import NewType

from scs.indexing.repository_paths import canonicalize_repo_path

StoreId = NewType("StoreId", str)
StoreGeneration = NewType("StoreGeneration", str)

_STORE_ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


class StoreState(StrEnum):
    """Lifecycle state recorded in the central project-store catalog."""

    UNINITIALIZED = "uninitialized"
    READY_STRUCTURAL = "ready_structural"
    SEMANTIC_STALE = "semantic_stale"
    SEMANTIC_READY = "semantic_ready"
    MIGRATING = "migrating"
    MIGRATION_FAILED_RECOVERABLE = "migration_failed_recoverable"


def canonical_repository_root(root: str | Path) -> str:
    """Return the sole catalog key for a repository root without writing state."""

    return canonicalize_repo_path(root)


def store_id_for_root(root: str | Path) -> StoreId:
    """Derive the opaque stable store identity from a canonical repository root.

    The digest prevents repository paths from becoming storage directory names,
    while determinism lets independent callers resolve the same root to the
    same store before the catalog exists.
    """

    canonical_root = canonical_repository_root(root)
    return StoreId(hashlib.sha256(canonical_root.encode("utf-8")).hexdigest())


def validate_store_id(value: StoreId) -> StoreId:
    """Reject a store identifier that cannot safely be a directory component."""

    if not _STORE_ID_PATTERN.fullmatch(value):
        raise ValueError("store_id must be a 64-character lowercase SHA-256 digest")
    return value


def validate_store_generation(value: StoreGeneration) -> StoreGeneration:
    """Reject generation values that could escape an isolated store directory."""

    if not _GENERATION_PATTERN.fullmatch(value):
        raise ValueError(
            "store generation must be 1-64 lowercase alphanumeric, '_' or '-' characters"
        )
    return value
