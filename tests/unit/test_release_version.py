"""Release version identity validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "check-release-version.py"
    spec = importlib.util.spec_from_file_location("check_release_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_release_versions_are_identical() -> None:
    module = _module()
    root = Path(__file__).parents[2]

    assert module.validate("v0.1.3", root) == "0.1.3"
    assert module.project_versions(root) == {
        "python": "0.1.3",
        "rust": "0.1.3",
        "runtime": "0.1.3",
    }


def test_release_tag_mismatch_is_rejected() -> None:
    module = _module()

    with pytest.raises(ValueError, match="does not match"):
        module.validate("v9.9.9", Path(__file__).parents[2])
