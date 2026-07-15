"""The SCS CLI exposes every operational command without a UI dependency."""

from __future__ import annotations

from scs.cli import build_parser


def test_operational_commands_are_parseable() -> None:
    parser = build_parser()
    assert parser.parse_args(["serve"]).command == "serve"
    assert parser.parse_args(["doctor"]).command == "doctor"
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["index", "."]).command == "index"
    assert parser.parse_args(["reindex", "."]).command == "reindex"
    for action in ("install", "start", "stop", "restart", "status", "uninstall"):
        assert parser.parse_args(["service", action]).action == action
