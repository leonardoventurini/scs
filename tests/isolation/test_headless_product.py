"""SCS remains a headless service without an accidental UI product surface."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_manifest_and_tree_have_no_ui_framework_or_web_pipeline() -> None:
    root = Path(__file__).parents[2]
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = "\n".join(manifest["project"]["dependencies"]).lower()
    forbidden_dependencies = ("react", "svelte", "vite", "tauri", "django", "flask")
    assert not any(name in dependencies for name in forbidden_dependencies)

    forbidden_paths = ("webview", "frontend", "dashboard", "src-tauri", "package.json")
    relative_paths = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    assert not any(
        relative == forbidden or relative.startswith(f"{forbidden}/")
        for forbidden in forbidden_paths
        for relative in relative_paths
    )
