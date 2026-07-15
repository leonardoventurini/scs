"""The SCS CLI exposes every operational command without a UI dependency."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from scs.cli import build_parser, main


def test_operational_commands_are_parseable() -> None:
    parser = build_parser()
    assert parser.parse_args(["serve"]).command == "serve"
    assert parser.parse_args(["doctor"]).command == "doctor"
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["index", "."]).command == "index"
    assert parser.parse_args(["reindex", "."]).command == "reindex"
    for action in ("install", "start", "stop", "restart", "status", "uninstall"):
        assert parser.parse_args(["service", action]).action == action


def test_proxy_entrypoint_is_installed_with_root_scs_package() -> None:
    from scs_mcp_proxy.main import main as proxy_main

    assert callable(proxy_main)


def test_proxy_command_runs_installed_proxy_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_proxy_main(arguments: tuple[str, ...] = ()) -> int:
        calls.append(tuple(arguments))
        return 17

    monkeypatch.setattr("scs_mcp_proxy.main.main", fake_proxy_main)

    assert main(["proxy"]) == 17
    assert calls == [()]


@dataclass(frozen=True)
class _ServiceStatus:
    proxy_loaded: bool = True
    daemon_loaded: bool = True


class _ServiceManager:
    def status(self) -> _ServiceStatus:
        return _ServiceStatus()


class _Paths:
    def __init__(self, home: Path) -> None:
        self.home = home

    def ensure(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)


class _Settings:
    def __init__(self, home: Path) -> None:
        self.paths = _Paths(home)


@pytest.mark.parametrize("command", ["doctor", "status"])
def test_operational_commands_report_daemon_unavailable_as_json(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def unavailable(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise FileNotFoundError("scs.sock is unavailable")

    monkeypatch.setattr("scs.cli._call_daemon", unavailable)
    monkeypatch.setattr("scs.cli.SCSSettings", lambda: _Settings(tmp_path / "home"))
    monkeypatch.setattr("scs.cli.ServiceManager", _ServiceManager)

    assert main([command]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["daemon"]["available"] is False
    assert payload["daemon"]["ready"] is False
    assert payload["daemon"]["error"] == "FileNotFoundError"
    if command == "status":
        assert payload["launchd"] == {"proxy_loaded": True, "daemon_loaded": True}
