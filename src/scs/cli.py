"""Headless command-line interface for SCS operations and ownership."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import cast

from scs import __version__
from scs.config import SCSSettings
from scs.daemon import DaemonController
from scs.main import serve
from scs.mcp.stdio import serve_stdio
from scs.storage.snapshot import list_saved_projects
from scs.wire.client import SCSConnection


def build_parser() -> argparse.ArgumentParser:
    """Build the complete non-graphical SCS command contract."""

    parser = argparse.ArgumentParser(
        prog="scs", description="Semantic Code Search service"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve", help="run the SCS daemon in the foreground")
    subcommands.add_parser("mcp", help="run a lazy per-harness MCP stdio bridge")
    subcommands.add_parser("doctor", help="validate storage and daemon health")
    subcommands.add_parser("status", help="show daemon state")
    subcommands.add_parser("version", help="show the installed SCS version")
    metrics = subcommands.add_parser(
        "metrics", help="show privacy-preserving aggregate operation metrics"
    )
    metrics.add_argument("--days", type=int, default=7)
    metrics.add_argument("--json", action="store_true", dest="json")

    index = subcommands.add_parser("index", help="explicitly index a repository")
    index.add_argument("repo_path", type=Path)
    reindex = subcommands.add_parser(
        "reindex", help="explicitly rebuild a repository index"
    )
    reindex.add_argument("repo_path", type=Path)
    projects = subcommands.add_parser("list", help="list enrolled projects")
    projects.add_argument("--json", action="store_true", dest="json")
    delete = subcommands.add_parser("delete", help="delete one enrolled project")
    delete.add_argument("selector", help="numeric project ID or repository path")
    reingest = subcommands.add_parser(
        "reingest", help="fully reingest one enrolled project"
    )
    reingest.add_argument("selector", help="numeric project ID or repository path")

    daemon = subcommands.add_parser("daemon", help="manage the shared lazy daemon")
    daemon.add_argument(
        "action", choices=("start", "stop", "restart", "status")
    )
    daemon.add_argument(
        "--cancel-active",
        action="store_true",
        help="cancel active indexing before stopping (for upgrades)",
    )
    return parser


async def _call_daemon(
    method: str, params: dict[str, object] | None = None
) -> dict[str, object]:
    settings = SCSSettings()
    await DaemonController(settings).ensure_started()
    async with SCSConnection(settings.paths.runtime / "scs.sock") as connection:
        return await connection.call(method, params)


def _selector_params(selector: object) -> dict[str, object]:
    """Parse an unambiguous numeric ID or preserve a path selector."""

    if not isinstance(selector, str) or not selector:
        raise ValueError("project selector must be a non-empty ID or path")
    if selector.lstrip("-").isdecimal():
        project_id = int(selector)
        if project_id < 1:
            raise ValueError("project ID must be positive")
        return {"project_id": project_id}
    return {"repo_path": selector}


def _project_table(payload: dict[str, object]) -> str:
    """Render stable columns while allowing paths to retain their full value."""

    raw_projects = payload.get("projects")
    if not isinstance(raw_projects, list):
        raise ValueError("project listing must contain a projects array")
    headers = ("ID", "STATE", "FILES", "LAST INDEXED", "PATH")
    rows: list[tuple[str, str, str, str, str]] = [headers]
    for raw_project in cast(list[object], raw_projects):
        if not isinstance(raw_project, dict):
            raise ValueError("project listing entries must be objects")
        project = cast(dict[str, object], raw_project)
        rows.append(
            (
                str(project.get("id", "")),
                str(project.get("state", "")),
                str(project.get("file_count", 0)),
                str(project.get("last_indexed") or "-"),
                str(project.get("repo_path", "")),
            )
        )
    widths = [max(len(row[index]) for row in rows) for index in range(len(headers))]
    return "\n".join(
        "  ".join(
            value.ljust(widths[index]) if index < len(headers) - 1 else value
            for index, value in enumerate(row)
        ).rstrip()
        for row in rows
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one CLI command and return a process exit code."""

    arguments = build_parser().parse_args(argv)
    values = cast(dict[str, object], vars(arguments))
    command = values.get("command")
    if command == "serve":
        asyncio.run(serve())
        return 0
    if command == "mcp":
        asyncio.run(serve_stdio())
        return 0
    if command == "version":
        print(__version__)
        return 0
    if command == "daemon":
        controller = DaemonController()
        action = values.get("action")
        if action == "start":
            result = asyncio.run(controller.ensure_started())
        elif action == "stop":
            stopped = asyncio.run(
                controller.stop(cancel_active=values.get("cancel_active") is True)
            )
            result = asyncio.run(controller.status())
            print(json.dumps({"stopped": stopped, **asdict(result)}, sort_keys=True))
            return 0
        elif action == "restart":
            asyncio.run(controller.stop())
            result = asyncio.run(controller.ensure_started())
        elif action == "status":
            result = asyncio.run(controller.status())
        else:
            raise AssertionError(f"unhandled daemon action: {action}")
        print(json.dumps(asdict(result), sort_keys=True))
        return 0 if result.ready else 1
    if command == "doctor":
        settings = SCSSettings()
        try:
            settings.paths.ensure()
        except Exception as error:
            print(
                json.dumps(
                    {
                        "storage": {
                            "available": False,
                            "error": type(error).__name__,
                            "message": str(error),
                        },
                        "daemon": {"available": False, "ready": False},
                    },
                    sort_keys=True,
                )
            )
            return 1
        try:
            result = asyncio.run(_call_daemon("system.health"))
        except Exception as error:
            print(
                json.dumps(
                    {
                        "storage": {
                            "available": True,
                            "home": str(settings.paths.home),
                        },
                        "daemon": {
                            "available": False,
                            "ready": False,
                            "error": type(error).__name__,
                            "message": str(error),
                        },
                    },
                    sort_keys=True,
                )
            )
            return 1
        print(
            json.dumps(
                {
                    "storage": {"available": True, "home": str(settings.paths.home)},
                    "daemon": {"available": True, **result},
                },
                sort_keys=True,
            )
        )
        return 0 if result.get("ready") is True else 1
    if command == "status":
        daemon_status = asyncio.run(DaemonController().status())
        print(json.dumps({"daemon": asdict(daemon_status)}, sort_keys=True))
        return 0 if daemon_status.ready else 1
    if command == "metrics":
        raw_days = values.get("days", 7)
        if not isinstance(raw_days, int):
            raise AssertionError("metrics days must be an integer")
        result = asyncio.run(_call_daemon("metrics.report", {"days": raw_days}))
        # The command is intentionally machine-readable by default; --json is
        # retained as an explicit stable contract for automation.
        print(json.dumps(result, sort_keys=True))
        return 0
    if command == "list":
        result = list_saved_projects(SCSSettings().paths.home)
        if values.get("json") is True:
            print(json.dumps(result, sort_keys=True))
        else:
            print(_project_table(result))
        return 0
    if command in {"delete", "reingest"}:
        method = f"project.{command}"
        result = asyncio.run(
            _call_daemon(method, _selector_params(values.get("selector")))
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    if command in {"index", "reindex"}:
        repo_path = values.get("repo_path")
        if not isinstance(repo_path, Path):
            raise AssertionError("index command requires a repository path")
        method = f"repository.{command}"
        result = asyncio.run(_call_daemon(method, {"repo_path": str(repo_path)}))
        print(json.dumps(result, sort_keys=True))
        return 0
    raise AssertionError(f"unhandled SCS command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
