from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from scs.config import SCSSettings
from scs.paths import UnsafeStorageRootError, validate_scs_home


def test_defaults_are_scs_owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    settings = SCSSettings()

    assert settings.home == tmp_path / "user" / ".scs"
    assert "External product" not in str(settings.paths.home)
    assert settings.paths.database.name == "index.db"
    assert not settings.paths.home.exists()
    assert settings.mcp_internal_host == "127.0.0.1"
    assert settings.mcp_internal_port == 28465


def test_internal_mcp_endpoint_is_typed_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCS_MCP_INTERNAL_HOST", "localhost")
    monkeypatch.setenv("SCS_MCP_INTERNAL_PORT", "0")

    settings = SCSSettings()

    assert settings.mcp_internal_host == "localhost"
    assert settings.mcp_internal_port == 0


def test_rejects_legacy_root_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(legacy, target_is_directory=True)
    monkeypatch.setenv("EXTERNAL_PRODUCT_HOME", str(legacy))

    with pytest.raises(UnsafeStorageRootError, match="overlaps"):
        validate_scs_home(alias)


@pytest.mark.parametrize("relative", [Path("child"), Path("..")])
def test_rejects_nested_or_containing_legacy_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: Path,
) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setenv("EXTERNAL_PRODUCT_HOME", str(legacy))

    with pytest.raises(UnsafeStorageRootError, match="overlaps"):
        validate_scs_home((legacy / relative).resolve())


def test_rejects_external_volume_symlink_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    external = tmp_path / "Volumes" / "Data" / "external-product"
    external.mkdir(parents=True)
    link = tmp_path / "mounted-external-product"
    link.symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("EXTERNAL_PRODUCT_HOME", str(link))

    with pytest.raises(UnsafeStorageRootError, match="overlaps"):
        validate_scs_home(external)


def test_permission_denied_marker_check_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "scs"
    home.mkdir()
    original_scandir = os.scandir

    def denied(path: str | os.PathLike[str]) -> object:
        if Path(path) == home:
            raise PermissionError("denied")
        return original_scandir(path)

    with patch("scs.paths.os.scandir", side_effect=denied):
        with pytest.raises(UnsafeStorageRootError, match="Cannot verify"):
            validate_scs_home(home)


def test_marker_detection_precedes_writes(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    (unsafe / "brain.db").touch()

    with pytest.raises(UnsafeStorageRootError, match="legacy External product"):
        SCSSettings(home=unsafe).paths.ensure()

    assert not (unsafe / "index.db").exists()
