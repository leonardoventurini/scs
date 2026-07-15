"""SCS-owned filesystem paths and legacy-data isolation guards."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

LEGACY_EXTERNAL_PRODUCT_MARKERS: frozenset[str] = frozenset(
    {
        "brain.db",
        "brain.db-wal",
        "brain.db-shm",
        "brain.usearch",
    }
)


class UnsafeStorageRootError(RuntimeError):
    """Raised before writes when an SCS root aliases legacy External product storage."""


def _resolved(path: Path) -> Path:
    """Resolve aliases without requiring the final path to exist."""

    return path.expanduser().resolve(strict=False)


def _contains_path(container: Path, candidate: Path) -> bool:
    """Return whether ``candidate`` equals or is nested under ``container``."""

    try:
        candidate.relative_to(container)
    except ValueError:
        return False
    return True


def _configured_legacy_roots() -> tuple[Path, ...]:
    """Return known legacy roots, resolving external-volume symlink aliases."""

    roots = [Path.home() / ".external-product"]
    configured = os.environ.get("EXTERNAL_PRODUCT_HOME")
    if configured:
        roots.append(Path(configured))
    return tuple(dict.fromkeys(_resolved(root) for root in roots))


def _assert_marker_state_readable(root: Path) -> None:
    """Fail closed if a potentially relevant directory cannot be inspected."""

    if not root.exists():
        return
    try:
        with os.scandir(root) as entries:
            names = {entry.name for entry in entries}
    except OSError as exc:
        raise UnsafeStorageRootError(
            f"Cannot verify SCS storage safety at {root}: {exc}"
        ) from exc
    markers = sorted(names & LEGACY_EXTERNAL_PRODUCT_MARKERS)
    if markers:
        raise UnsafeStorageRootError(
            f"SCS_HOME aliases legacy External product storage at {root}; found {', '.join(markers)}"
        )


def validate_scs_home(home: Path) -> Path:
    """Validate and return a canonical SCS root without creating any files.

    The guard rejects equality, ancestry, or descendants shared with a known
    External product data root. It also inspects every existing ancestor for legacy
    markers. Validation deliberately precedes all SCS directory creation.
    """

    canonical = _resolved(home)
    legacy_roots = _configured_legacy_roots()
    for legacy in legacy_roots:
        if _contains_path(legacy, canonical) or _contains_path(canonical, legacy):
            raise UnsafeStorageRootError(
                f"SCS_HOME {canonical} overlaps legacy External product root {legacy}"
            )

    existing_ancestors = [candidate for candidate in (canonical, *canonical.parents) if candidate.exists()]
    for candidate in existing_ancestors:
        _assert_marker_state_readable(candidate)

    return canonical


@dataclass(frozen=True, slots=True)
class SCSPaths:
    """Validated, product-owned persistent and ephemeral paths."""

    home: Path
    database: Path
    vector_index: Path
    jobs_database: Path
    provider_metadata: Path
    model_cache: Path
    runtime: Path
    logs: Path

    @classmethod
    def resolve(
        cls,
        home: Path,
        *,
        model_cache: Path | None = None,
        runtime: Path | None = None,
        logs: Path | None = None,
    ) -> "SCSPaths":
        """Build path values after validating the persistent root."""

        safe_home = validate_scs_home(home)
        return cls(
            home=safe_home,
            database=safe_home / "index.db",
            vector_index=safe_home / "index.usearch",
            jobs_database=safe_home / "jobs.db",
            provider_metadata=safe_home / "provider.json",
            model_cache=_resolved(model_cache or (Path.home() / ".cache" / "scs" / "models")),
            runtime=_resolved(
                runtime
                or (Path.home() / "Library" / "Application Support" / "SCS")
            ),
            logs=_resolved(logs or (Path.home() / "Library" / "Logs" / "SCS")),
        )

    def ensure(self) -> None:
        """Create SCS-owned directories only after repeating the safety gate."""

        validate_scs_home(self.home)
        for directory in (self.home, self.model_cache, self.runtime, self.logs):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
