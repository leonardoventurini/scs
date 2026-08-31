from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from scs.config import SCSSettings
from scs.paths import UnsafeStorageRootError, validate_scs_home


def test_defaults_are_scs_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_text_ingestion_limits_have_typed_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCS_INDEX_TEXT_FALLBACK", "false")
    monkeypatch.setenv("SCS_INDEX_MAX_FILE_BYTES", "2048")
    monkeypatch.setenv("SCS_INDEX_TEXT_SAMPLE_BYTES", "512")
    monkeypatch.setenv("SCS_INDEX_LARGE_DIR_FILES", "300")
    monkeypatch.setenv("SCS_INDEX_LARGE_DIR_BYTES", "4096")

    settings = SCSSettings()

    assert settings.index_text_fallback is False
    assert settings.index_max_file_bytes == 2048
    assert settings.index_text_sample_bytes == 512
    assert settings.index_large_dir_files == 300
    assert settings.index_large_dir_bytes == 4096


def test_text_sample_cannot_exceed_maximum_file_size() -> None:
    with pytest.raises(ValueError, match="sample"):
        SCSSettings(index_max_file_bytes=128, index_text_sample_bytes=256)


def test_automatic_reindex_settings_have_adaptive_defaults() -> None:
    settings = SCSSettings()

    assert settings.auto_reindex_enabled is True
    assert settings.auto_reindex_active_seconds == 2.0
    assert settings.auto_reindex_idle_seconds == 30.0
    assert settings.auto_reindex_debounce_seconds == 0.5
    assert settings.auto_reindex_git_timeout_seconds == 10.0


def test_automatic_reindex_settings_accept_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCS_AUTO_REINDEX_ENABLED", "false")
    monkeypatch.setenv("SCS_AUTO_REINDEX_ACTIVE_SECONDS", "3")
    monkeypatch.setenv("SCS_AUTO_REINDEX_IDLE_SECONDS", "45")
    monkeypatch.setenv("SCS_AUTO_REINDEX_DEBOUNCE_SECONDS", "1.25")
    monkeypatch.setenv("SCS_AUTO_REINDEX_GIT_TIMEOUT_SECONDS", "12")

    settings = SCSSettings()

    assert settings.auto_reindex_enabled is False
    assert settings.auto_reindex_active_seconds == 3.0
    assert settings.auto_reindex_idle_seconds == 45.0
    assert settings.auto_reindex_debounce_seconds == 1.25
    assert settings.auto_reindex_git_timeout_seconds == 12.0


def test_automatic_reindex_idle_interval_cannot_be_shorter_than_active() -> None:
    with pytest.raises(ValueError, match="idle interval"):
        SCSSettings(
            auto_reindex_active_seconds=10.0,
            auto_reindex_idle_seconds=5.0,
        )


def test_omlx_defaults_target_the_verified_local_embedding_service() -> None:
    settings = SCSSettings()

    assert settings.embedding_provider == "omlx"
    assert settings.embedding_model == "Qwen3-Embedding-8B-4bit-DWQ"
    assert settings.embedding_dimension == 4096
    assert settings.omlx_base_url == "http://127.0.0.1:10000/v1"


def test_omlx_rejects_remote_embedding_endpoint() -> None:
    with pytest.raises(ValueError, match="loopback"):
        SCSSettings(omlx_base_url="http://embeddings.example.com/v1")


def test_rejects_legacy_root_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_rejects_external_volume_symlink_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
