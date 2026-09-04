"""SCS-owned filesystem paths."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

def default_runtime_directory() -> Path:
    """Return a private platform-appropriate runtime directory."""

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SCS"
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        return Path(configured) / "scs"
    return Path.home() / ".local" / "state" / "scs" / "runtime"


def default_log_directory() -> Path:
    """Return the platform-appropriate persistent log directory."""

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "SCS"
    return Path.home() / ".local" / "state" / "scs" / "logs"


def _resolved(path: Path) -> Path:
    """Resolve aliases without requiring the final path to exist."""

    return path.expanduser().resolve(strict=False)


def validate_scs_home(home: Path) -> Path:
    """Return the canonical SCS root without creating files."""

    return _resolved(home)


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
                runtime or default_runtime_directory()
            ),
            logs=_resolved(logs or default_log_directory()),
        )

    def ensure(self) -> None:
        """Create SCS-owned directories only after repeating the safety gate."""

        validate_scs_home(self.home)
        for directory in (self.home, self.model_cache, self.runtime, self.logs):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
