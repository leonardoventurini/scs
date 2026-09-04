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
    assert parser.parse_args(["mcp"]).command == "mcp"
    assert parser.parse_args(["doctor"]).command == "doctor"
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["version"]).command == "version"
    assert parser.parse_args(["index", "."]).command == "index"
    assert parser.parse_args(["reindex", "."]).command == "reindex"
    for action in ("start", "stop", "restart", "status"):
        assert parser.parse_args(["daemon", action]).action == action


def test_mcp_entrypoint_is_installed_with_root_scs_package() -> None:
    from scs.mcp.stdio import main as mcp_main

    assert callable(mcp_main)


def test_mcp_command_runs_installed_stdio_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    async def fake_serve_stdio() -> None:
        calls.append(True)

    monkeypatch.setattr("scs.cli.serve_stdio", fake_serve_stdio)

    assert main(["mcp"]) == 0
    assert calls == [True]


@dataclass(frozen=True)
class _ServiceStatus:
    available: bool = False
    ready: bool = False
    pid: int | None = None
    generation: str | None = None
    version: str | None = None
    error: str | None = "FileNotFoundError"


class _DaemonController:
    async def status(self) -> _ServiceStatus:
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
    monkeypatch.setattr("scs.cli.DaemonController", _DaemonController)

    assert main([command]) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["daemon"]["available"] is False
    assert payload["daemon"]["ready"] is False
    assert payload["daemon"]["error"] == "FileNotFoundError"
    if command == "status":
        assert payload["daemon"]["available"] is False
