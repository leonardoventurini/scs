"""Uninstallation removes only the executing tool and explicitly selected state."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import scs.uninstall as module
from scs.config import SCSSettings
from scs.service import ProcessLock
from scs.storage.catalog import ProjectStoreCatalog


@dataclass
class Sandbox:
    settings: SCSSettings
    user_home: Path
    prefix: Path
    files: tuple[Path, ...]
    events: list[str]
    lock_inodes: dict[Path, int]


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    user_home = tmp_path / "user"
    prefix = user_home / "tools" / "scs"
    prefix.mkdir(parents=True)
    (prefix / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    package_file = prefix / "lib" / "scs" / "uninstall.py"
    package_file.parent.mkdir(parents=True)
    package_file.touch()

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: user_home))
    monkeypatch.setattr(module.sys, "prefix", str(prefix))
    monkeypatch.setattr(module, "__file__", str(package_file))
    monkeypatch.setattr(module.shutil, "which", lambda _command: "/fake/bin/uv")

    settings = SCSSettings(
        home=user_home / "data",
        runtime_dir=user_home / "runtime",
        model_cache=user_home / "cache",
        log_dir=user_home / "logs",
    )
    files: list[Path] = []
    for index, directory in enumerate(
        (settings.home, settings.runtime_dir, settings.model_cache, settings.log_dir)
    ):
        directory.mkdir()
        file = directory / f"state-{index}"
        file.write_text(str(index), encoding="utf-8")
        files.append(file)

    config = user_home / ".scs" / "config.toml"
    config.parent.mkdir()
    config.write_text("", encoding="utf-8")
    files.append(config)
    events: list[str] = []
    lock_inodes: dict[Path, int] = {}

    class Controller:
        def __init__(self, actual: SCSSettings) -> None:
            assert actual is settings

        async def stop(self, *, cancel_active: bool = False) -> bool:
            assert cancel_active is True
            with pytest.raises(RuntimeError):
                with ProcessLock(settings.paths.runtime / ".bootstrap.lock"):
                    pytest.fail("bootstrap lock must protect shutdown")
            events.append("stop")
            return True

    def remove(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert command == ["/fake/bin/uv", "tool", "uninstall", "scs"]
        assert env["UV_TOOL_DIR"] == str(prefix.parent)
        assert check is True
        with pytest.raises(RuntimeError):
            with ProcessLock(settings.paths.home / ".daemon.lock"):
                pytest.fail("writer lock must protect removal")
        assert all(file.exists() for file in files)
        for lock in (
            settings.paths.home / ".daemon.lock",
            settings.paths.runtime / ".bootstrap.lock",
        ):
            lock_inodes[lock] = lock.stat().st_ino
        events.append("remove")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module, "DaemonController", Controller)
    monkeypatch.setattr(module.subprocess, "run", remove)

    return Sandbox(settings, user_home, prefix, tuple(files), events, lock_inodes)


def test_default_uninstall_preserves_all_state_and_selects_current_tool(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = [file.read_bytes() for file in sandbox.files]
    monkeypatch.setenv("UV_TOOL_DIR", str(sandbox.user_home / "another-tool-root"))

    module.uninstall(sandbox.settings)

    assert sandbox.events == ["stop", "remove"]
    assert [file.read_bytes() for file in sandbox.files] == contents


def test_purge_removes_custom_state_and_fixed_config_but_preserves_sources(
    sandbox: Sandbox,
) -> None:
    source = sandbox.user_home / "source" / "sample.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    (sandbox.settings.model_cache / "source-link").symlink_to(source.parent)
    other_index = sandbox.user_home / ".scs" / "index.db"
    other_index.touch()
    mcp_config = sandbox.user_home / ".codex" / "config.toml"
    mcp_config.parent.mkdir()
    mcp_config.write_text('[mcp_servers.scs]\ncommand = "scs"\n', encoding="utf-8")
    mcp_contents = mcp_config.read_bytes()

    module.uninstall(sandbox.settings, purge=True)

    assert sandbox.events == ["stop", "remove"]
    assert all(not file.exists() for file in sandbox.files)
    assert source.read_text(encoding="utf-8") == "value = 1\n"
    assert other_index.exists()
    for lock in (
        sandbox.settings.paths.home / ".daemon.lock",
        sandbox.settings.paths.runtime / ".bootstrap.lock",
    ):
        assert lock.is_file()
        assert lock.stat().st_ino == sandbox.lock_inodes[lock]
        with ProcessLock(lock):
            pass
    assert not sandbox.settings.model_cache.exists()
    assert not sandbox.settings.log_dir.exists()
    assert mcp_config.read_bytes() == mcp_contents


@pytest.mark.parametrize("reason", ["uv", "receipt", "source"])
def test_installation_preflight_failure_has_no_side_effects(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    if reason == "uv":
        monkeypatch.setattr(module.shutil, "which", lambda _command: None)
    elif reason == "receipt":
        (sandbox.prefix / "uv-receipt.toml").unlink()
    else:
        monkeypatch.setattr(module, "__file__", str(sandbox.user_home / "source.py"))

    with pytest.raises(module.UninstallError):
        module.uninstall(sandbox.settings, purge=True)

    assert sandbox.events == []
    assert all(file.exists() for file in sandbox.files)
    assert not (sandbox.settings.paths.runtime / ".bootstrap.lock").exists()


@pytest.mark.parametrize("lock_name", ["bootstrap", "writer"])
def test_lifecycle_contention_prevents_package_removal(
    sandbox: Sandbox, lock_name: str
) -> None:
    lock_path = (
        sandbox.settings.paths.runtime / ".bootstrap.lock"
        if lock_name == "bootstrap"
        else sandbox.settings.paths.home / ".daemon.lock"
    )

    with ProcessLock(lock_path):
        with pytest.raises(module.UninstallError):
            module.uninstall(sandbox.settings, purge=True)

    assert "remove" not in sandbox.events
    assert all(file.exists() for file in sandbox.files)


def test_failed_shutdown_preserves_package_and_data(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Controller:
        def __init__(self, _settings: SCSSettings) -> None:
            pass

        async def stop(self, *, cancel_active: bool = False) -> bool:
            raise TimeoutError("writer still active")

    monkeypatch.setattr(module, "DaemonController", Controller)

    with pytest.raises(module.UninstallError, match="writer still active"):
        module.uninstall(sandbox.settings, purge=True)

    assert sandbox.events == []
    assert all(file.exists() for file in sandbox.files)


def test_failed_package_removal_never_purges_data(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(7, ["uv", "tool", "uninstall", "scs"])

    monkeypatch.setattr(module.subprocess, "run", fail)

    with pytest.raises(module.UninstallError, match="package removal"):
        module.uninstall(sandbox.settings, purge=True)

    assert sandbox.events == ["stop"]
    assert all(file.exists() for file in sandbox.files)


@pytest.mark.parametrize(
    "target",
    [
        "root",
        "home",
        "shared",
        "documents",
        "tool",
        "other-tool",
        "symlink",
        "repo",
        "enrolled",
    ],
)
def test_unsafe_purge_paths_fail_before_shutdown(sandbox: Sandbox, target: str) -> None:
    path = sandbox.user_home / "unsafe"
    if target == "root":
        path = Path("/")
    elif target == "home":
        path = sandbox.user_home
    elif target == "shared":
        path = sandbox.user_home / ".cache"
    elif target == "documents":
        path = sandbox.user_home / "Documents"
    elif target == "tool":
        path = sandbox.prefix
    elif target == "other-tool":
        path = sandbox.prefix.parent / "other"
    elif target == "symlink":
        path.symlink_to(sandbox.settings.model_cache, target_is_directory=True)
    elif target == "repo":
        (path / "nested" / ".git").mkdir(parents=True)
    else:
        path.mkdir()
        ProjectStoreCatalog(sandbox.settings.home).register(path)

    settings = sandbox.settings.model_copy(update={"model_cache": path})
    # model_copy must not retain a cached path value from the original settings.
    settings.__dict__.pop("paths", None)

    with pytest.raises(module.UninstallError, match="purge"):
        module.uninstall(settings, purge=True)

    assert sandbox.events == []
    assert all(file.exists() for file in sandbox.files)


def test_nested_and_missing_cleanup_roots_are_supported(sandbox: Sandbox) -> None:
    sandbox.settings.runtime_dir = sandbox.settings.home / "nested" / "runtime"
    sandbox.settings.runtime_dir.mkdir(parents=True)
    sandbox.settings.log_dir = sandbox.settings.home / "logs"
    sandbox.settings.model_cache = sandbox.settings.home / "absent-models"
    sandbox.settings.log_dir.mkdir()
    (sandbox.settings.log_dir / "daemon.log").touch()

    module.uninstall(sandbox.settings, purge=True)

    assert not sandbox.settings.log_dir.exists()
    assert not sandbox.settings.model_cache.exists()
    assert sandbox.events == ["stop", "remove"]
    for lock, inode in sandbox.lock_inodes.items():
        assert lock.stat().st_ino == inode


def test_default_data_root_purge_removes_config_with_its_other_contents(
    sandbox: Sandbox,
) -> None:
    config = sandbox.files[-1]
    sandbox.settings.home = config.parent
    sandbox.settings.__dict__.pop("paths", None)

    module.uninstall(sandbox.settings, purge=True)

    assert not config.exists()
    assert sandbox.events == ["stop", "remove"]


@pytest.mark.parametrize("relationship", ["ancestor", "equal", "descendant"])
def test_purge_never_overlaps_enrolled_unversioned_source(
    sandbox: Sandbox, relationship: str
) -> None:
    repository = sandbox.user_home / "workspace" / "plain-source"
    assets = repository / "assets"
    assets.mkdir(parents=True)
    source = assets / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    ProjectStoreCatalog(sandbox.settings.home).register(repository)
    sandbox.settings.model_cache = {
        "ancestor": repository.parent,
        "equal": repository,
        "descendant": assets,
    }[relationship]

    with pytest.raises(module.UninstallError, match="purge"):
        module.uninstall(sandbox.settings, purge=True)

    assert sandbox.events == []
    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_corrupt_catalog_is_reported_before_shutdown(sandbox: Sandbox) -> None:
    (sandbox.settings.home / "catalog.db").write_bytes(bytes(range(128)))

    with pytest.raises(module.UninstallError, match="purge validation"):
        module.uninstall(sandbox.settings, purge=True)

    assert sandbox.events == []
    assert all(file.exists() for file in sandbox.files)


@pytest.mark.parametrize("purge", [False, True])
@pytest.mark.parametrize("lock_name", ["bootstrap", "writer"])
def test_lock_symlinks_never_modify_their_external_targets(
    sandbox: Sandbox, purge: bool, lock_name: str
) -> None:
    target = sandbox.user_home / "source.txt"
    target.write_text("source must remain intact\n", encoding="utf-8")
    lock = (
        sandbox.settings.runtime_dir / ".bootstrap.lock"
        if lock_name == "bootstrap"
        else sandbox.settings.home / ".daemon.lock"
    )
    lock.symlink_to(target)

    with pytest.raises(module.UninstallError, match="lock symlink"):
        module.uninstall(sandbox.settings, purge=purge)

    assert target.read_text(encoding="utf-8") == "source must remain intact\n"
    assert sandbox.events == []


def test_cleanup_failure_reports_package_removal_and_remaining_paths(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    (sandbox.settings.home / "projects").mkdir()

    def fail(_path: Path) -> None:
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(module.shutil, "rmtree", fail)

    with pytest.raises(
        module.UninstallError, match="package removed.*cleanup"
    ) as error:
        module.uninstall(sandbox.settings, purge=True)

    assert str(sandbox.settings.home) in str(error.value)
    assert sandbox.events == ["stop", "remove"]
