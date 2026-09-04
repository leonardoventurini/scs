#!/usr/bin/env python3
"""Validate that a release tag matches every SCS version source."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

VERSION_PATTERN = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)


def project_versions(root: Path) -> dict[str, str]:
    """Read version identities that must move together for a release."""

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    cargo = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    package_source = (root / "src" / "scs" / "__init__.py").read_text(
        encoding="utf-8"
    )
    package_match = VERSION_PATTERN.search(package_source)
    if package_match is None:
        raise ValueError("src/scs/__init__.py has no static __version__")
    return {
        "python": str(pyproject["project"]["version"]),
        "rust": str(cargo["workspace"]["package"]["version"]),
        "runtime": package_match.group(1),
    }


def validate(tag: str, root: Path) -> str:
    """Return the normalized version or raise on any mismatch."""

    if not tag.startswith("v") or len(tag) == 1:
        raise ValueError("release tag must have the form v<version>")
    version = tag[1:]
    versions = project_versions(root)
    mismatches = {name: value for name, value in versions.items() if value != version}
    if mismatches:
        details = ", ".join(f"{name}={value}" for name, value in mismatches.items())
        raise ValueError(f"tag {tag} does not match {details}")
    return version


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        print("usage: check-release-version.py TAG", file=sys.stderr)
        return 2
    try:
        print(validate(arguments[1], Path(__file__).resolve().parents[1]))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
