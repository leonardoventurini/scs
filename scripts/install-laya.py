"""Explicitly acquire the pinned Laya bundle in SCS's model cache."""

from __future__ import annotations

from scs.config import SCSSettings
from scs.orchestration.bundle import install_bundle


def main() -> None:
    print(install_bundle(SCSSettings().paths.model_cache))


if __name__ == "__main__":
    main()
