"""The SCS CLI exposes every operational command without a UI dependency."""

from __future__ import annotations

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
