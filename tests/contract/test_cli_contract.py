"""The SCS CLI exposes every operational command without a UI dependency."""

from __future__ import annotations

import json
import sqlite3
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
    assert parser.parse_args(["list"]).command == "list"
    assert parser.parse_args(["list", "--json"]).json is True
    assert parser.parse_args(["delete", "12"]).selector == "12"
    assert parser.parse_args(["reingest", "/tmp/repo"]).selector == "/tmp/repo"
    metrics = parser.parse_args(["metrics", "--days", "14", "--json"])
    assert metrics.command == "metrics"
    assert metrics.days == 14
    assert metrics.json is True
    for action in ("start", "stop", "restart", "status"):
        assert parser.parse_args(["daemon", action]).action == action
    stop = parser.parse_args(["daemon", "stop", "--cancel-active"])
    assert stop.action == "stop"
    assert stop.cancel_active is True


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


def test_metrics_command_reads_daemon_aggregates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def report(method: str, params: dict[str, object]) -> dict[str, object]:
        assert method == "metrics.report"
        assert params == {"days": 14}
        return {"days": 14, "totals": {"calls": 2, "errors": 0}, "operations": []}

    monkeypatch.setattr("scs.cli._call_daemon", report)

    assert main(["metrics", "--days", "14", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["calls"] == 2


def test_list_reads_saved_index_without_starting_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scs.storage.catalog import ProjectStoreCatalog
    from scs.storage.models import StoreGeneration, StoreState
    from scs.storage.paths import ProjectStorePaths

    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    catalog = ProjectStoreCatalog(home)
    registered = catalog.register(repo)
    generation = StoreGeneration("gtest")
    paths = ProjectStorePaths.resolve(home, registered.store_id, generation)
    paths.ensure()
    catalog.activate(repo, generation=generation, state=StoreState.SEMANTIC_STALE)
    with sqlite3.connect(paths.database) as connection:
        connection.execute("CREATE TABLE scopes (id INTEGER PRIMARY KEY, key TEXT NOT NULL)")
        connection.execute("CREATE TABLE catalog (namespace TEXT, key TEXT, value TEXT)")
        connection.execute("INSERT INTO scopes (key) VALUES (?)", (str(repo),))
        connection.execute(
            "INSERT INTO catalog VALUES (?, ?, ?)",
            (
                "scs.ingested-files",
                "sample.py",
                json.dumps({"repo_path": str(repo), "indexed_at": "2026-09-22T12:00:00Z"}),
            ),
        )

    empty_repo = tmp_path / "empty-repo"
    empty_repo.mkdir()
    empty_record = catalog.register(empty_repo)
    empty_paths = ProjectStorePaths.resolve(home, empty_record.store_id, generation)
    empty_paths.ensure()
    catalog.activate(
        empty_repo, generation=generation, state=StoreState.SEMANTIC_READY
    )
    with sqlite3.connect(empty_paths.database) as connection:
        connection.execute("CREATE TABLE catalog (namespace TEXT, key TEXT, value TEXT)")

    async def unexpected_daemon_call(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("listing must not start or call the daemon")

    monkeypatch.setenv("SCS_HOME", str(home))
    monkeypatch.setattr("scs.cli._call_daemon", unexpected_daemon_call)

    assert main(["list"]) == 0
    table = capsys.readouterr().out
    assert "ID" in table and "indexed" in table and str(repo) in table
    assert main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "projects": [
            {
                "id": registered.project_id,
                "state": "indexed",
                "file_count": 1,
                "last_indexed": "2026-09-22T12:00:00Z",
                "repo_path": str(repo),
                "active_job_id": None,
            },
            {
                "id": empty_record.project_id,
                "state": "indexed",
                "file_count": 0,
                "last_indexed": None,
                "repo_path": str(empty_repo),
                "active_job_id": None,
            },
        ]
    }


def test_list_of_missing_catalog_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SCS_HOME", str(home))

    assert main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"projects": []}
    assert not home.exists()


@pytest.mark.parametrize(
    ("command", "selector", "method", "expected"),
    [
        ("delete", "7", "project.delete", {"project_id": 7}),
        ("delete", "/tmp/repo", "project.delete", {"repo_path": "/tmp/repo"}),
        ("reingest", "7", "project.reingest", {"project_id": 7}),
        ("reingest", "/tmp/repo", "project.reingest", {"repo_path": "/tmp/repo"}),
    ],
)
def test_project_lifecycle_commands_dispatch_numeric_ids_or_paths(
    command: str,
    selector: str,
    method: str,
    expected: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def call(actual: str, params: dict[str, object]) -> dict[str, object]:
        assert actual == method
        assert params == expected
        return {"accepted": True, "job": {"id": "job-1"}}

    monkeypatch.setattr("scs.cli._call_daemon", call)

    assert main([command, selector]) == 0
    assert json.loads(capsys.readouterr().out)["accepted"] is True


@pytest.mark.parametrize("selector", ["0", "-1"])
def test_project_lifecycle_commands_reject_non_positive_ids(selector: str) -> None:
    with pytest.raises(ValueError, match="project ID must be positive"):
        main(["delete", selector])


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


def test_daemon_stop_forwards_upgrade_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[bool] = []

    class Controller:
        async def stop(self, *, cancel_active: bool = False) -> bool:
            calls.append(cancel_active)
            return True

        async def status(self) -> _ServiceStatus:
            return _ServiceStatus()

    monkeypatch.setattr("scs.cli.DaemonController", Controller)

    assert main(["daemon", "stop", "--cancel-active"]) == 0
    assert calls == [True]
    assert json.loads(capsys.readouterr().out)["stopped"] is True


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
